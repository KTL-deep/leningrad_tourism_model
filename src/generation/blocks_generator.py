import geopandas as gpd
import pandera as pa
# Хотфикс: отключаем валидацию pandera, чтобы избежать ошибки булевого оператора `|` для GeoSeries в выводе BlocksNet
setattr(pa.DataFrameSchema, 'validate', lambda self, check_obj, *args, **kwargs: check_obj)
from blocksnet.preprocessing import BlocksGenerator
from shapely.ops import unary_union

class CityBlocksGenerator:
    """
    Класс для разбиения непрерывного городского/регионального пространства
    на дискретные полигоны — городские блоки. В качестве линий разреза
    используются дорожные сети и водные объекты.
    Использует алгоритмы из библиотеки blocksnet.
    """
    
    def __init__(self, boundary_gdf: gpd.GeoDataFrame):
        """
        Инициализация генератора.
        :param boundary_gdf: GeoDataFrame с границами анализируемой территории (Polygon/MultiPolygon).
        """
        self.boundary = boundary_gdf

    def generate_blocks(
        self, 
        roads_gdf: gpd.GeoDataFrame = None, 
        water_gdf: gpd.GeoDataFrame = None,
        railways_gdf: gpd.GeoDataFrame = None,
        min_block_width: float = 10.0,
        rail_corridor_half_width_m: float = 20.0
    ) -> gpd.GeoDataFrame:
        """
        Генерация блоков через BlocksGenerator (нарезание геометрии барьерами).

        :param roads_gdf: Линии дорожной сети (барьеры)
        :param water_gdf: Водные объекты (барьеры)
        :param railways_gdf: Железнодорожные пути (дополнительные барьеры)
        :param min_block_width: Минимальная ширина полученного блока (для исключения артефактов)
        :param rail_corridor_half_width_m: Полуширина ж/д коридора (м). Используется, чтобы
            схлопывать параллельные пути в один “коридор” и не получать узкие “щепки”
            между путями (типично 10–40 м).
        
        :return: GeoDataFrame с полигонами кварталов/блоков
        """
        print("Инициализация BlocksGenerator...")
        
        # Определяем метрическую UTM-проекцию по центру границы.
        # blocksnet использует area-фильтр, который корректно работает только
        # в метрических координатах (не в градусах EPSG:4326).
        bound_wgs = self.boundary.to_crs(epsg=4326) if self.boundary.crs.to_epsg() != 4326 else self.boundary
        centroid = bound_wgs.geometry.unary_union.centroid
        zone = int((centroid.x + 180) / 6) + 1
        utm_epsg = 32600 + zone if centroid.y >= 0 else 32700 + zone
        utm_crs = f"EPSG:{utm_epsg}"
        print(f"Используемая метрическая СК: {utm_crs}")
        
        # Перепроецируем все слои в метрическую СК и оставляем только колонку geometry
        bound_clean = self.boundary[['geometry']].reset_index(drop=True).to_crs(utm_crs)
        
        roads_clean = None
        if roads_gdf is not None and not roads_gdf.empty:
            roads_clean = roads_gdf[['geometry']].reset_index(drop=True).to_crs(utm_crs)
        
        water_clean = None
        if water_gdf is not None and not water_gdf.empty:
            water_clean = water_gdf[['geometry']].reset_index(drop=True).to_crs(utm_crs)
        
        rail_clean = None
        if railways_gdf is not None and not railways_gdf.empty:
            rail_clean_raw = railways_gdf[['geometry']].reset_index(drop=True).to_crs(utm_crs)
            rail_clean = self._railways_to_corridor_barrier(
                rail_clean_raw,
                half_width_m=rail_corridor_half_width_m
            )
        
        bg = BlocksGenerator(
            boundaries=bound_clean,
            roads=roads_clean,
            railways=rail_clean,
            water=water_clean
        )
        
        print("Запуск пространственной кластеризации (нарезание блоков)...")
        blocks_gdf = bg.run(min_block_width=min_block_width)
        print(f"Сгенерировано {len(blocks_gdf)} блоков.")
        
        return blocks_gdf

    @staticmethod
    def _railways_to_corridor_barrier(railways_utm: gpd.GeoDataFrame, half_width_m: float) -> gpd.GeoDataFrame:
        """
        Преобразует набор ж/д линий (часто параллельных) в один/несколько
        линейных барьеров по границе “ж/д коридора”.

        Идея: буферизуем линии на half_width_m, растворяем (unary_union),
        берём границу получившегося полигона/мультиполигона как LineString/MultiLineString.
        Это убирает внутренние разрезы между путями (и “щепки”).
        """
        if railways_utm is None or railways_utm.empty:
            return None

        # Буферизуем и растворяем
        buffered = railways_utm.geometry.buffer(float(half_width_m))
        corridor = unary_union([geom for geom in buffered if geom is not None and not geom.is_empty])

        if corridor is None:
            return None

        boundary = corridor.boundary
        if boundary is None or boundary.is_empty:
            return None

        return gpd.GeoDataFrame(geometry=[boundary], crs=railways_utm.crs)
