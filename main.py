import sys
import os

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
from src.generation.blocks_generator import CityBlocksGenerator
from src.generation.ucm_builder import UCMBuilder

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

def generate_ucm(region_name: str, output_path: str = "data/processed/ucm_blocks.geojson"):
    """
    Главный пайплайн генерации UCM.
    :param region_name: Название региона (например "Siversky, Leningrad Oblast, Russia")
    :param output_path: Путь для сохранения итогового слоя блоков
    """
    print(f"=== Запуск сборки UCM для {region_name} ===")
    
    # 1. Загрузка данных
    osm = OSMLoader(location_name=region_name)
    gis = GISLoader(data_dir="data/raw")
    
    # Получаем базовую геометрию (границы)
    boundary_gdf = osm.get_boundary()
    _safe_export(boundary_gdf, "data/processed/osm/boundary.geojson")
    
    # Загружаем кадастровые участки и кварталы
    cadastre_gdf = gis.load_cadastral_data("cadastre.geojson")
    _safe_export(cadastre_gdf, "data/processed/gis/cadastre.geojson")

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

    # 2. Генерация блоков (каскадная генерация: Кадастр -> Landuse -> Сетка)
    generator = CityBlocksGenerator(boundary_gdf=boundary_gdf)
    blocks_gdf = generator.generate_blocks(
        cadastre_gdf=cadastre_gdf,
        landuse_gdf=land_use_gdf,
        min_area_m2=10.0,
        grid_cell_size=50.0
    )
    
    # 3. Атрибутирование блоков (UCM)
    builder = UCMBuilder(blocks_gdf=blocks_gdf)
    builder.attribute_land_use(landuse_gdf=land_use_gdf)
    builder.attribute_amenities(amenities_gdf=amenities_gdf)
    builder.attribute_cultural_heritage(okn_gdf=okn_gdf)
    builder.attribute_protected_areas(oopt_gdf=oopt_gdf)
    
    # 4. Сохранение
    builder.export_to_geojson(filepath=output_path)
    print(f"=== Генерация UCM успешно завершена. Файл: {output_path} ===")
    
    # 5. Сценарное математическое взвешивание AHP (Шаг 3)
    try:
        from src.analysis.ahp import run_stage2_ahp
        from pathlib import Path
        print("\n=== Запуск сценарного взвешивания (AHP) ===")
        run_stage2_ahp(
            blocks_path=Path(output_path),
            constants_path=Path("configs/ahp_constants.json"),
            output_csv=Path("data/processed/ahp_block_scores.csv"),
            output_geojson=Path("data/processed/ucm_blocks_with_attractiveness.geojson")
        )
    except Exception as e:
        print(f"⚠️ Ошибка при расчете AHP: {e}")

if __name__ == "__main__":
    # Для теста будем использовать небольшой населенный пункт в Ленинградской области
    # чтобы пайплайн отработал быстро
    test_region = "Рощино, Ленинградская область, Россия"
    generate_ucm(region_name=test_region)
