import sys
import os

# Принудительно переключаем вывод на UTF-8, чтобы кириллица корректно
# отображалась в Windows-терминале (по умолчанию используется cp1252)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import geopandas as gpd

from src.etl.osm_loader import OSMLoader
from src.etl.gis_loader import GISLoader
from src.generation.blocks_generator import CityBlocksGenerator
from src.generation.ucm_builder import UCMBuilder

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
    
    # Загружаем барьеры (дороги и вода)
    try:
        roads_gdf = osm.get_roads()
        print(f"Получено {len(roads_gdf)} сегментов дорог.")
    except Exception as e:
        print(f"Дороги не найдены: {e}")
        roads_gdf = None
        
    try:
        water_gdf = osm.get_water()
        print(f"Получено {len(water_gdf)} водных объектов.")
    except Exception as e:
        print(f"Водные объекты не найдены: {e}")
        water_gdf = None

    # Дополнительные слои для атрибутирования
    try:
        land_use_gdf = osm.get_land_use()
    except:
        land_use_gdf = gpd.GeoDataFrame()
        
    try:
        amenities_gdf = osm.get_amenities_and_buildings()
    except:
        amenities_gdf = gpd.GeoDataFrame()

    okn_gdf = gis.load_cultural_heritage()
    # oopt_gdf = gis.load_protected_areas() # можно добавить позже

    # 2. Генерация блоков
    generator = CityBlocksGenerator(boundary_gdf=boundary_gdf)
    blocks_gdf = generator.generate_blocks(
        roads_gdf=roads_gdf,
        water_gdf=water_gdf,
        min_block_width=5.0
    )
    
    # 3. Атрибутирование блоков (UCM)
    builder = UCMBuilder(blocks_gdf=blocks_gdf)
    builder.attribute_land_use(landuse_gdf=land_use_gdf)
    builder.attribute_amenities(amenities_gdf=amenities_gdf)
    builder.attribute_cultural_heritage(okn_gdf=okn_gdf)
    
    # 4. Сохранение
    builder.export_to_geojson(filepath=output_path)
    print(f"=== Генерация UCM успешно завершена. Файл: {output_path} ===")

if __name__ == "__main__":
    # Для теста будем использовать небольшой населенный пункт в Ленинградской области
    # чтобы пайплайн отработал быстро
    test_region = "Рощино, Ленинградская область, Россия"
    generate_ucm(region_name=test_region)
