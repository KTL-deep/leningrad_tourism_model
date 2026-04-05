import geopandas as gpd
import numpy as np
from shapely.geometry import box

class CityBlocksGenerator:
    """
    Класс для подготовки дискретных полигонов (кадастровых блоков) для анализа.
    Обрезает кадастровые участки по границе целевой территории и очищает мелкие топологические артефакты.
    """
    
    def __init__(self, boundary_gdf: gpd.GeoDataFrame):
        """
        Инициализация генератора.
        :param boundary_gdf: GeoDataFrame с границами анализируемой территории (Polygon/MultiPolygon).
        """
        self.boundary = boundary_gdf

    def _generate_grid(self, bound_clean: gpd.GeoDataFrame, utm_crs: str, cell_size_m: float) -> gpd.GeoDataFrame:
        """
        Генерирует аналитическую сетку, покрывающую переданный экстент.
        """
        bounds = bound_clean.total_bounds
        minx, miny, maxx, maxy = bounds
        
        x_coords = np.arange(minx, maxx, cell_size_m)
        y_coords = np.arange(miny, maxy, cell_size_m)
        
        polygons = []
        for x in x_coords:
            for y in y_coords:
                polygons.append(box(x, y, x + cell_size_m, y + cell_size_m))
                
        grid_gdf = gpd.GeoDataFrame({'geometry': polygons}, crs=utm_crs)
        return grid_gdf

    def generate_blocks(
        self, 
        cadastre_gdf: gpd.GeoDataFrame = None,
        landuse_gdf: gpd.GeoDataFrame = None,
        min_area_m2: float = 10.0,
        grid_cell_size: float = 50.0
    ) -> gpd.GeoDataFrame:
        """
        Подготовка кадастровых блоков или их альтернатив (Fallback).
        
        :param cadastre_gdf: Слой кадастровых участков (наивысший приоритет).
        :param landuse_gdf: Слой типов землепользования OSM (резервный вариант 1).
        :param min_area_m2: Минимальная площадь получаемого блока (кв.м.).
        :param grid_cell_size: Размер стороны ячейки аналитической сетки в метрах (резервный вариант 2).
        
        :return: GeoDataFrame с полигонами пространственных блоков
        """
        print("Инициализация генератора кадастровых блоков...")
        
        # Определяем метрическую UTM-проекцию по центру границы.
        bound_wgs = self.boundary.to_crs(epsg=4326) if self.boundary.crs.to_epsg() != 4326 else self.boundary
        centroid = bound_wgs.geometry.unary_union.centroid
        zone = int((centroid.x + 180) / 6) + 1
        utm_epsg = 32600 + zone if centroid.y >= 0 else 32700 + zone
        utm_crs = f"EPSG:{utm_epsg}"
        print(f"Используемая метрическая СК: {utm_crs}")
        
        # Перепроецируем границу
        bound_clean = self.boundary[['geometry']].reset_index(drop=True).to_crs(utm_crs)
        
        has_cadastre = cadastre_gdf is not None and not cadastre_gdf.empty
        has_landuse = landuse_gdf is not None and not landuse_gdf.empty
        
        if has_cadastre:
            print("🟢 Обнаружены кадастровые данные. Проецирование контуров...")
            sources_utm = cadastre_gdf.to_crs(utm_crs)
        elif has_landuse:
            print("🟡 Кадастр отсутствует! Fallback 1: Используем контуры землепользования (OSM Landuse) как блоки...")
            sources_utm = landuse_gdf.to_crs(utm_crs)
        else:
            print(f"🔴 Входных векторных данных нет! Fallback 2: Генерируем сплошную аналитическую сетку {grid_cell_size}x{grid_cell_size} м...")
            sources_utm = self._generate_grid(bound_clean, utm_crs, cell_size_m=grid_cell_size)
            
        print("Обрезка блоков по границе целевой территории (intersection)...")
        # Обрезаем источники границей (пересечение)
        try:
            blocks_gdf = gpd.overlay(sources_utm, bound_clean, how='intersection', keep_geom_type=False)
            # Оставляем только полигоны
            blocks_gdf = blocks_gdf[blocks_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        except Exception as e:
            print(f"Ошибка при пересечении блоков с границей: {e}")
            blocks_gdf = sources_utm
            
        print("Базовая проверка валидности геометрий и фильтрация артефактов...")
        blocks_gdf.geometry = blocks_gdf.geometry.make_valid()
        blocks_gdf = blocks_gdf[blocks_gdf.geometry.is_valid & ~blocks_gdf.geometry.is_empty]
        
        # Фильтрация артефактов ("щепок") по площади (geometry.area возвращает кв.м. в UTM)
        areas = blocks_gdf.geometry.area
        blocks_gdf = blocks_gdf[areas >= min_area_m2].copy()
        
        # Сбрасываем индексы после всех фильтраций
        blocks_gdf = blocks_gdf.reset_index(drop=True)
        
        print("Присвоение стабильных идентификаторов (block_id)...")
        blocks_gdf['block_id'] = range(len(blocks_gdf))
        
        print(f"Сгенерировано {len(blocks_gdf)} кадастровых блоков (размером >= {min_area_m2} кв.м.).")
        
        return blocks_gdf
