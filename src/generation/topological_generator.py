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
        print("Скачивание физических барьеров с учетом переменного масштаба...")

        # Применяем выбранный Overpass-сервер
        import os as _os
        overpass_url = _os.environ.get("OVERPASS_URL")
        if overpass_url and hasattr(ox.settings, "overpass_url"):
            ox.settings.overpass_url = overpass_url

        ox.settings.requests_timeout = 600

        hubs_gdf = self._fetch_hubs()
        
        roads_list = []
        # Фильтрация дорог: для BlocksNet нужны LineString
        # Мы фечим граф, так как это надежнее для дорожной сети
        for poly in self.boundary_gdf.geometry:
            try:
                print(f"  Загрузка графа дорог для части территории...")
                # Fetching ALL roads as a graph first (more robust)
                G = ox.graph_from_polygon(poly, network_type='all', simplify=True, retain_all=True)
                _, edges = ox.graph_to_gdfs(G)
                
                # Фильтруем типы дорог, которые нам нужны как барьеры
                keep_highways = [
                    'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 
                    'unclassified', 'residential', 'living_street', 'service'
                ]
                if 'highway' in edges.columns:
                    edges = edges[edges['highway'].apply(
                        lambda x: any(h in x for h in keep_highways) if isinstance(x, list) else x in keep_highways
                    )]
                
                roads_list.append(edges)
            except Exception as e:
                print(f"  [!] Ошибка загрузки графа дорог: {e}. Пробуем features_from_polygon...")
                try:
                    f_roads = ox.features_from_polygon(poly, {'highway': True})
                    f_roads = f_roads[f_roads.geometry.type.isin(['LineString', 'MultiLineString'])]
                    roads_list.append(f_roads)
                except Exception as e2:
                    print(f"  [!] Критическая ошибка загрузки дорог: {e2}")

        roads_gdf = pd.concat(roads_list, ignore_index=True) if roads_list else None
        
        # Применяем фильтр переменного масштаба (буфер вокруг ТПУ)
        if roads_gdf is not None and hubs_gdf is not None and not hubs_gdf.empty:
            print("Применение фильтрации дорог для переменного масштаба...")
            local_crs = self.boundary_gdf.estimate_utm_crs()
            hubs_proj = hubs_gdf.to_crs(local_crs)
            hubs_buffer = hubs_proj.buffer(3000).to_crs(epsg=4326).union_all()
            
            major_highways = ['motorway', 'trunk', 'primary', 'secondary']
            
            def should_keep(row):
                hw = row.get('highway')
                if isinstance(hw, list):
                    if any(h in major_highways for h in hw): return True
                elif hw in major_highways:
                    return True
                return row.geometry.intersects(hubs_buffer)
            
            roads_gdf['keep'] = roads_gdf.apply(should_keep, axis=1)
            roads_gdf = roads_gdf[roads_gdf['keep']].drop(columns=['keep']).copy()
            print(f"  Дороги отфильтрованы. Осталось: {len(roads_gdf)}")

        # 2. Железные дороги
        rail_list = []
        rail_tags = {'railway': ['rail', 'light_rail', 'narrow_gauge']}
        for poly in self.boundary_gdf.geometry:
            try:
                f_rail = ox.features_from_polygon(poly, rail_tags)
                f_rail = f_rail[f_rail.geometry.type.isin(['LineString', 'MultiLineString'])]
                rail_list.append(f_rail)
            except Exception as e:
                print(f"  [!] Ошибка загрузки ж/д данных: {e}")
        rail_gdf = pd.concat(rail_list, ignore_index=True) if rail_list else None

        # 3. Водные объекты
        water_list = []
        water_tags = {'waterway': ['river', 'stream', 'canal']}
        for poly in self.boundary_gdf.geometry:
            try:
                f_water = ox.features_from_polygon(poly, water_tags)
                f_water = f_water[f_water.geometry.type.isin(['LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'])]
                water_list.append(f_water)
            except Exception as e:
                print(f"  [!] Ошибка загрузки водных объектов: {e}")
        water_gdf = pd.concat(water_list, ignore_index=True) if water_list else None

        return roads_gdf, rail_gdf, water_gdf

    def _fetch_hubs(self):
        """
        Скачивает ТПУ (железнодорожные станции и вокзалы) для определения опорных центров.
        """
        print("Поиск ТПУ (вокзалы, станции) для определения опорных центров...")
        query_poly = self.boundary_gdf.unary_union
        hubs_tags = {'railway': ['station', 'halt']}
        try:
            hubs_gdf = ox.features_from_polygon(query_poly, hubs_tags)
            # Оставляем только точечные объекты (центроиды станций)
            # Перепроецируем для корректного расчета центроида
            if not hubs_gdf.empty:
                local_crs = hubs_gdf.estimate_utm_crs()
                hubs_gdf['geometry'] = hubs_gdf.to_crs(local_crs).geometry.centroid.to_crs(epsg=4326)
            hubs_gdf = hubs_gdf.reset_index(drop=True)
            return hubs_gdf[['geometry']]
        except Exception as e:
            print(f"  [!] Ошибка при поиске ТПУ: {e}")
            return None

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
        
        # Получаем ТПУ
        hubs_gdf = self._fetch_hubs()
        
        # Расчет расстояния до ближайшего ТПУ (для будущего анализа)
        if hubs_gdf is not None and not hubs_gdf.empty:
            print("Расчет расстояния от блоков до ближайшего ТПУ...")
            # Работаем в метрической проекции
            hubs_proj = hubs_gdf.to_crs(blocks_gdf.crs)
            # Для каждого блока находим расстояние до ближайшей точки ТПУ
            blocks_gdf['dist_to_hubs'] = blocks_gdf.geometry.centroid.apply(
                lambda x: hubs_proj.distance(x).min()
            ).round(2)
        else:
            blocks_gdf['dist_to_hubs'] = 0.0

        # Возвращаем в EPSG:4326 для совместимости с остальной системой
        blocks_gdf = blocks_gdf.to_crs(epsg=4326)
        
        # Сохраняем нужные колонки
        blocks_gdf = blocks_gdf[['block_id', 'dist_to_hubs', 'geometry']]
        print("=== Топологическая генерация завершена ===")
        
        return blocks_gdf
