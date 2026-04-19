import sys
import os
import time
from contextlib import contextmanager
import geopandas as gpd

from src.etl.osm_loader import OSMLoader
from src.etl.gis_loader import GISLoader
from src.generation.topological_generator import TopologicalGenerator
from src.generation.ucm_builder import UCMBuilder
from main import _safe_export
import iduedu
import pandas as pd
import numpy as np

# Настройка кодировки для Windows консоли
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

class Profiler:
    def __init__(self):
        self.records = []

    @contextmanager
    def measure(self, name: str, is_substage: bool = False):
        start_time = time.time()
        prefix = "  └─ " if is_substage else "▶ "
        print(f"{prefix}Начат{' подэтап' if is_substage else ' этап'}: {name}...")
        
        yield
        
        elapsed = time.time() - start_time
        self.records.append({"name": name, "elapsed": elapsed, "is_substage": is_substage})
        print(f"{prefix}Завершен{' подэтап' if is_substage else ' этап'}: {name}. Время: {elapsed:.2f} сек")

    def print_report(self):
        print("\n" + "="*50)
        print("📊 ОТЧЕТ О ВРЕМЕНИ ВЫПОЛНЕНИЯ ПАЙПЛАЙНА")
        print("="*50)
        
        total_time = 0
        for record in self.records:
            indent = "    ├─ " if record["is_substage"] else "■ "
            if not record["is_substage"]:
                total_time += record["elapsed"]
                
            print(f"{indent}{record['name']:<40} : {record['elapsed']:.2f} сек")
            
        print("="*50)
        print(f"■ ИТОГОВОЕ ВРЕМЯ (СУММА ОСНОВНЫХ ЭТАПОВ):    {total_time:.2f} сек")
        print("="*50)


def run_benchmark(region_names: list, output_path: str = "data/processed/ucm_blocks.geojson"):
    print(f"\n=== Запуск пайплайна с профилированием времени для: {region_names} ===\n")
    profiler = Profiler()
    
    with profiler.measure("1. Загрузка данных (ETL)"):
        osm = OSMLoader(locations=region_names)
        gis = GISLoader(data_dir="data/raw")
        
        with profiler.measure("Извлечение границ территории (OSM)", is_substage=True):
            boundary_gdf = osm.get_boundary()
            _safe_export(boundary_gdf, "data/processed/osm/boundary.geojson")
        
        with profiler.measure("Загрузка Landuse (OSM)", is_substage=True):
            try:
                land_use_gdf = osm.get_land_use()
                _safe_export(land_use_gdf, "data/processed/osm/landuse.geojson")
            except Exception as e:
                print(f"  [!] Ошибка при загрузке landuse: {e}")
                land_use_gdf = gpd.GeoDataFrame()
                
        with profiler.measure("Загрузка Amenities/Buildings (OSM)", is_substage=True):
            try:
                amenities_gdf = osm.get_amenities_and_buildings()
                _safe_export(amenities_gdf, "data/processed/osm/amenities_buildings.geojson")
            except Exception as e:
                print(f"  [!] Ошибка при загрузке amenities: {e}")
                amenities_gdf = gpd.GeoDataFrame()
                
        with profiler.measure("Загрузка ОКН (GIS)", is_substage=True):
            okn_gdf = gis.load_cultural_heritage()
            _safe_export(okn_gdf, "data/processed/gis/okn.geojson")
            
        with profiler.measure("Загрузка ООПТ (GIS)", is_substage=True):
            oopt_gdf = gis.load_protected_areas()
            _safe_export(oopt_gdf, "data/processed/gis/oopt.geojson")

    with profiler.measure("2. Топологическая генерация блоков"):
        generator = TopologicalGenerator(boundary_gdf=boundary_gdf)
        blocks_gdf = generator.generate_blocks(min_area_m2=500.0)
        _safe_export(blocks_gdf, "data/processed/topological_blocks.geojson")

    with profiler.measure("3. Атрибутирование блоков (UCMBuilder)"):
        builder = UCMBuilder(blocks_gdf=blocks_gdf)
        
        with profiler.measure("Привязка Landuse", is_substage=True):
            builder.attribute_land_use(landuse_gdf=land_use_gdf)
            
        with profiler.measure("Привязка Amenities", is_substage=True):
            builder.attribute_amenities(amenities_gdf=amenities_gdf)
            
        with profiler.measure("Привязка культурного наследия", is_substage=True):
            builder.attribute_cultural_heritage(okn_gdf=okn_gdf)
            
        with profiler.measure("Привязка природных территорий", is_substage=True):
            builder.attribute_protected_areas(oopt_gdf=oopt_gdf)

    with profiler.measure("4. Сохранение файла UCM (GeoJSON)"):
        builder.export_to_geojson(filepath=output_path)

    with profiler.measure("5. Расчет транспортной доступности (iduedu)"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with profiler.measure("Построение графа автодорог (Drive Graph)", is_substage=True):
                boundary_poly_4326 = boundary_gdf.to_crs(epsg=4326).unary_union
                intermodal_graph = iduedu.get_drive_graph(polygon=boundary_poly_4326)
                graph_crs = intermodal_graph.graph.get('crs', 4326)
            
            with profiler.measure("Подготовка геометрии (Центроиды)", is_substage=True):
                blocks_for_matrix = builder.get_ucm().copy()
                if not blocks_for_matrix.crs.is_projected:
                    blocks_for_matrix = blocks_for_matrix.to_crs(blocks_for_matrix.estimate_utm_crs())
                blocks_for_matrix['geometry'] = blocks_for_matrix.geometry.centroid
                blocks_for_matrix = blocks_for_matrix.to_crs(graph_crs)
            
            with profiler.measure("Вычисление матрицы (get_adj_matrix_gdf_to_gdf)", is_substage=True):
                acc_matrix = iduedu.get_adj_matrix_gdf_to_gdf(
                    gdf_from=blocks_for_matrix,
                    gft_to=blocks_for_matrix,
                    nx_graph=intermodal_graph,
                    weight='time_min',
                    dtype=np.float32
                )
            
            with profiler.measure("Сохранение матрицы (Parquet)", is_substage=True):
                matrix_path = "data/processed/accessibility_matrix.parquet"
                acc_matrix.to_parquet(matrix_path)

    print("\n[TODO] Сценарное взвешивание и оптимизация будут интегрированы на следующих этапах.\n")

    # Вывод финального отчета
    profiler.print_report()

if __name__ == "__main__":
    test_regions = [
        "Рощино, Ленинградская область, Россия",
        "Зеленогорск, Санкт-Петербург, Россия"
    ]
    run_benchmark(region_names=test_regions)
