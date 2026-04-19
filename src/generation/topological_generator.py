import geopandas as gpd
import pandas as pd
import osmnx as ox
import warnings
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from blocksnet import BlocksGenerator

class TopologicalGenerator:
    """
    Генератор городских блоков на основе физических преград (дороги, реки, ж/д)
    с использованием библиотеки BlocksNet.
    """
    def __init__(self, boundary_gdf: gpd.GeoDataFrame):
        """
        :param boundary_gdf: GeoDataFrame с границами исследуемой территории (EPSG:4326)
        """
        self.boundary_gdf = boundary_gdf
        if self.boundary_gdf.crs and self.boundary_gdf.crs.to_epsg() != 4326:
            self.boundary_gdf = self.boundary_gdf.to_crs(epsg=4326)
            
    def _fetch_physical_barriers(self):
        """
        Скачивает линии дорог, железных дорог и водных объектов через OSMnx
        для использования их в качестве барьеров при нарезке.
        """
        print("Скачивание физических барьеров (дороги, ж/д, реки) через OSMnx...")
        
        # Объединяем границы в один полигон
        query_poly = self.boundary_gdf.unary_union
        
        # 1. Дороги (магистрали и основные улицы)
        roads_tags = {
            'highway': ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 
                        'unclassified', 'residential']
        }
        try:
            roads_gdf = ox.features_from_polygon(query_poly, roads_tags)
            roads_gdf = roads_gdf[roads_gdf.geometry.type.isin(['LineString', 'MultiLineString'])]
            roads_gdf = roads_gdf.reset_index(drop=True)
        except Exception as e:
            print(f"  [!] Ошибка при загрузке дорог: {e}")
            roads_gdf = None

        # 2. Железные дороги
        rail_tags = {'railway': ['rail', 'light_rail', 'narrow_gauge']}
        try:
            rail_gdf = ox.features_from_polygon(query_poly, rail_tags)
            rail_gdf = rail_gdf[rail_gdf.geometry.type.isin(['LineString', 'MultiLineString'])]
            rail_gdf = rail_gdf.reset_index(drop=True)
        except Exception as e:
            print(f"  [!] Ошибка при загрузке ж/д: {e}")
            rail_gdf = None

        # 3. Водные объекты (реки)
        water_tags = {'waterway': ['river', 'stream', 'canal']}
        try:
            water_gdf = ox.features_from_polygon(query_poly, water_tags)
            water_gdf = water_gdf[water_gdf.geometry.type.isin(['LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'])]
            water_gdf = water_gdf.reset_index(drop=True)
        except Exception as e:
            print(f"  [!] Ошибка при загрузке водных объектов: {e}")
            water_gdf = None

        return roads_gdf, rail_gdf, water_gdf

    def generate_blocks(self, min_area_m2: float = 500.0) -> gpd.GeoDataFrame:
        """
        Запускает алгоритм диаграмм Вороного (BlocksNet) для нарезки территории.
        
        :param min_area_m2: Минимальная площадь блока в квадратных метрах.
                            Мелкие осколки (артефакты нарезки) будут отфильтрованы.
        :return: GeoDataFrame с готовыми топологическими блоками (EPSG:4326).
        """
        print("=== Старт генерации топологических блоков (BlocksNet) ===")
        roads_gdf, rail_gdf, water_gdf = self._fetch_physical_barriers()
        
        # Переводим всё в локальную метрическую систему координат для корректных расчетов площади внутри BlocksNet
        local_crs = self.boundary_gdf.estimate_utm_crs()
        boundaries_proj = self.boundary_gdf.to_crs(local_crs)
        roads_proj = roads_gdf.to_crs(local_crs) if roads_gdf is not None else None
        rail_proj = rail_gdf.to_crs(local_crs) if rail_gdf is not None else None
        water_proj = water_gdf.to_crs(local_crs) if water_gdf is not None else None

        print("Инициализация BlocksGenerator...")
        # BlocksGenerator сам проецирует данные в локальную метрическую систему при расчетах
        generator = BlocksGenerator(
            boundaries=boundaries_proj,
            roads=roads_proj,
            railways=rail_proj,
            water=water_proj
        )
        
        # Фикс бага интеграции pandera и geopandas в библиотеке blocksnet (GeoSeries.__or__ issue)
        import blocksnet.preprocessing.blocks_generator as bg
        original_schema = bg.BlocksSchema
        bg.BlocksSchema = lambda x: x
        
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # Запуск нарезки (BlocksGenerator возвращает GeoDataFrame в метрической системе)
                blocks_gdf = generator.run()
        finally:
            bg.BlocksSchema = original_schema
        
        if blocks_gdf is None or blocks_gdf.empty:
            raise ValueError("BlocksNet не смог сгенерировать ни одного блока.")
            
        # Фильтрация микро-полигонов (мусора)
        print(f"Фильтрация блоков площадью менее {min_area_m2} кв. м...")
        # Убедимся, что мы в метрической CRS перед расчетом площади
        if blocks_gdf.crs is None or not blocks_gdf.crs.is_projected:
            # BlocksGenerator обычно возвращает в спроецированной CRS, но для надежности
            blocks_gdf = blocks_gdf.to_crs(self.boundary_gdf.estimate_utm_crs())
            
        initial_count = len(blocks_gdf)
        blocks_gdf = blocks_gdf[blocks_gdf.geometry.area >= min_area_m2].copy()
        
        filtered_count = len(blocks_gdf)
        print(f"Отфильтровано {initial_count - filtered_count} осколков. Осталось: {filtered_count} блоков.")
        
        # Назначаем стабильные block_id
        blocks_gdf = blocks_gdf.reset_index(drop=True)
        blocks_gdf['block_id'] = blocks_gdf.index.astype(str)
        
        # Возвращаем в EPSG:4326 для совместимости с остальной системой
        blocks_gdf = blocks_gdf.to_crs(epsg=4326)
        
        # Сохраняем только нужные колонки
        blocks_gdf = blocks_gdf[['block_id', 'geometry']]
        print("=== Топологическая генерация завершена ===")
        
        return blocks_gdf
