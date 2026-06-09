import sys
import os

# Добавляем корень проекта в sys.path, чтобы избежать ошибки ModuleNotFoundError 
# и запускать скрипт без костылей вроде $env:PYTHONPATH="."
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Принудительно переключаем вывод на UTF-8, чтобы кириллица корректно
# отображалась в Windows-терминале и при редиректе в файл/пайп.
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    else:
        raise AttributeError("stderr has no reconfigure")
except Exception:
    import io

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import geopandas as gpd

from src.etl.osm_loader import OSMLoader
from src.etl.gis_loader import GISLoader
from src.generation.topological_generator import TopologicalGenerator
from src.generation.ucm_builder import UCMBuilder
import iduedu
import pandas as pd
import numpy as np

def _safe_export(gdf: gpd.GeoDataFrame, path: str) -> None:
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

def generate_ucm(region_names: list, output_path: str = "data/processed/ucm_blocks.geojson"):
    """
    Главный пайплайн генерации UCM.
    :param region_names: Список названий регионов (например ["Пушкинский район, Санкт-Петербург", "Гатчинский район, Ленинградская область"])
    :param output_path: Путь для сохранения итогового слоя блоков
    """
    import time
    start_time = time.time()
    print(f"=== Запуск сборки UCM для {region_names} ===")
    
    # 1. Загрузка данных
    osm = OSMLoader(locations=region_names)
    gis = GISLoader(data_dir="data/raw")
    
    # Получаем базовую геометрию (границы)
    boundary_gdf = osm.get_boundary()
    _safe_export(boundary_gdf, "data/processed/osm/boundary.geojson")
    
    # Дополнительные слои для атрибутирования
    try:
        land_use_gdf = osm.get_land_use()
        _safe_export(land_use_gdf, "data/processed/osm/landuse.geojson")
    except:
        land_use_gdf = gpd.GeoDataFrame()
        
    try:
        amenities_gdf = osm.get_amenities_and_buildings()
        _safe_export(amenities_gdf, "data/processed/osm/amenities_buildings.geojson")
    except:
        amenities_gdf = gpd.GeoDataFrame()

    okn_gdf = gis.load_cultural_heritage()
    _safe_export(okn_gdf, "data/processed/gis/okn.geojson")
    oopt_gdf = gis.load_protected_areas()
    _safe_export(oopt_gdf, "data/processed/gis/oopt.geojson")

    # 2. Топологическая генерация блоков (Этап 2)
    generator = TopologicalGenerator(boundary_gdf=boundary_gdf)
    blocks_gdf = generator.generate_blocks(min_area_m2=500.0)
    _safe_export(blocks_gdf, "data/processed/topological_blocks.geojson")

    # 3. Атрибутирование блоков (UCM) (Этап 3 - часть старого кода)
    builder = UCMBuilder(blocks_gdf=blocks_gdf)
    builder.attribute_land_use(landuse_gdf=land_use_gdf)
    
    # Извлекаем болота и воду для новых факторов
    if not land_use_gdf.empty:
        # Болота
        wetlands = land_use_gdf[land_use_gdf.get('natural') == 'wetland']
        builder.attribute_swampiness(wetlands_gdf=wetlands)
        
        # Водные объекты (полигоны и линии)
        water_tags = ['water', 'river', 'stream', 'canal', 'lake']
        is_water = pd.Series(False, index=land_use_gdf.index)
        for tag in ['natural', 'landuse', 'water', 'waterway']:
            if tag in land_use_gdf.columns:
                is_water |= land_use_gdf[tag].isin(water_tags) | land_use_gdf[tag].notna() if tag in ['water', 'waterway'] else land_use_gdf[tag].isin(water_tags)
        
        water_gdf = land_use_gdf[is_water]
        builder.attribute_water_density(water_gdf=water_gdf)

    builder.attribute_amenities(amenities_gdf=amenities_gdf)
    builder.attribute_cultural_heritage(okn_gdf=okn_gdf)
    builder.attribute_protected_areas(oopt_gdf=oopt_gdf)
    
    # 4. Сохранение
    builder.export_to_geojson(filepath=output_path)
    print(f"=== Генерация UCM успешно завершена. Файл: {output_path} ===")

    # 5. Матрица доступности (Этап 3)
    print("\n=== Старт расчета матрицы транспортной доступности ===")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # iduedu ожидает полигон в EPSG:4326
        boundary_poly_4326 = boundary_gdf.to_crs(epsg=4326).unary_union
        
        matrix_path = "data/processed/accessibility_matrix.parquet"
        try:
            # Для ускорения расчетов и избежания блокировок Overpass API (406 Not Acceptable),
            # мы используем автомобильный граф (Drive Graph) в качестве базового.
            print("Построение графа дорожной сети (iduedu.get_drive_graph)...")
            intermodal_graph = iduedu.get_drive_graph(territory=boundary_poly_4326)
            graph_crs = intermodal_graph.graph.get('crs', 4326)
            
            print("Экспорт транспортного графа для дашборда...")
            import osmnx as ox
            # iduedu.get_drive_graph возвращает nx.MultiDiGraph
            edges = ox.graph_to_gdfs(intermodal_graph, nodes=False)
            _safe_export(edges, "data/processed/drive_graph_edges.geojson")
            
            # Центроиды блоков для расчета матрицы
            blocks_for_matrix = builder.get_ucm().copy()
            if not blocks_for_matrix.crs.is_projected:
                blocks_for_matrix = blocks_for_matrix.to_crs(blocks_for_matrix.estimate_utm_crs())
            blocks_for_matrix['geometry'] = blocks_for_matrix.geometry.centroid
            blocks_for_matrix = blocks_for_matrix.to_crs(graph_crs)
            
            print("Расчет матрицы доступности (get_adj_matrix_gdf_to_gdf)...")
            # Вычисляем матрицу между всеми блоками
            acc_matrix = iduedu.get_adj_matrix_gdf_to_gdf(
                gdf_from=blocks_for_matrix,
                gft_to=blocks_for_matrix,
                nx_graph=intermodal_graph,
                weight='time_min',
                dtype=np.float32
            )
            acc_matrix.to_parquet(matrix_path)
            print(f"=== Матрица доступности успешно сохранена: {matrix_path} ===")
        except Exception as e:
            print(f"⚠️ Ошибка при построении графа или матрицы: {e}")
            print("Использование евклидова расстояния в качестве заглушки...")
            blocks_for_matrix = builder.get_ucm().copy()
            if not blocks_for_matrix.crs.is_projected:
                blocks_for_matrix = blocks_for_matrix.to_crs(blocks_for_matrix.estimate_utm_crs())
            
            # Создаем матрицу расстояний (в метрах)
            centroids = blocks_for_matrix.geometry.centroid
            n = len(centroids)
            dist_matrix = np.zeros((n, n), dtype=np.float32)
            for i in range(n):
                for j in range(n):
                    dist_matrix[i, j] = centroids.iloc[i].distance(centroids.iloc[j]) / 1000.0 * 10.0 # примерное время в мин
            
            ids = blocks_for_matrix.index.astype(str)
            acc_matrix = pd.DataFrame(dist_matrix, index=ids, columns=ids)
            acc_matrix.to_parquet(matrix_path)
            print(f"=== Матрица (Евклид) сохранена: {matrix_path} ===")

    # 6. Сценарное взвешивание AHP (Этап 4)
    print("\n=== Старт сценарного взвешивания (AHP) ===")
    from src.analysis.ahp import run_stage2_ahp
    from pathlib import Path
    
    ahp_csv_out = Path("data/processed/ahp_block_scores.csv")
    ahp_geojson_out = Path("data/processed/ucm_blocks_with_attractiveness.geojson")
    
    run_stage2_ahp(
        blocks_path=Path(output_path),
        constants_path=Path("configs/ahp_constants.json"),
        output_csv=ahp_csv_out,
        output_geojson=ahp_geojson_out
    )
    
    # 7. Глобальная оптимизация (Этап 5)
    print("\n=== Старт пространственной оптимизации (Simulated Annealing) ===")
    from src.analysis.optimizer import run_optimization
    
    opt_geojson_out = Path("data/processed/ucm_blocks_optimized.geojson")
    run_optimization(
        blocks_path=ahp_geojson_out,
        acc_matrix_path=Path(matrix_path),
        output_geojson=opt_geojson_out,
        max_iter=50000
    )
    
    elapsed_time = time.time() - start_time
    print(f"=== Выполнение скрипта завершено. Общее время: {elapsed_time:.2f} секунд ===")
    return

if __name__ == "__main__":
    # Пилотный полигон для валидации модели:
    # Сами города, а не целые районы, чтобы избежать таймаутов OSM и получить детальную сетку
    pilot_regions = [
        "Пушкин, Санкт-Петербург, Россия",
        "Гатчина, Ленинградская область, Россия"
    ]
    generate_ucm(region_names=pilot_regions)
