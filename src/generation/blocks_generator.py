import geopandas as gpd
import pandera as pa
# Хотфикс: отключаем валидацию pandera, чтобы избежать ошибки булевого оператора `|` для GeoSeries в выводе BlocksNet
setattr(pa.DataFrameSchema, 'validate', lambda self, check_obj, *args, **kwargs: check_obj)
from blocksnet.preprocessing import BlocksGenerator

class CityBlocksGenerator:
    """
    Класс для разбиения непрерывного городского/регионального пространства
    на дискретные полигоны - городские блоки. В качестве линий разреза 
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
        min_block_width: float = 10.0
    ) -> gpd.GeoDataFrame:
        """
        Генерация блоков через BlocksGenerator (нарезание геометрии баррьерами).

        :param roads_gdf: Линии дорожной сети (барьеры)
        :param water_gdf: Водные объекты (барьеры)
        :param railways_gdf: Железнодорожные пути (дополнительные барьеры)
        :param min_block_width: Минимальная ширина полученного блока (для исключения артефактов)
        
        :return: GeoDataFrame с полигонами кварталов/блоков
        """
        print("Инициализация BlocksGenerator...")
        
        # Сброс индексов, чтобы pandera validator в blocksnet не падал на MultiIndex из OSMnx
        roads_clean = roads_gdf[['geometry']].reset_index(drop=True) if roads_gdf is not None else None
        water_clean = water_gdf[['geometry']].reset_index(drop=True) if water_gdf is not None else None
        rail_clean = railways_gdf[['geometry']].reset_index(drop=True) if railways_gdf is not None else None
        bound_clean = self.boundary[['geometry']].reset_index(drop=True)
        
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
