import osmnx as ox
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon

class OSMLoader:
    """
    Класс для загрузки открытых пространственных данных из OSM.
    Используется для подготовки базовых слоев и физических барьеров 
    для генерации городских блоков.
    """
    
    def __init__(self, location_name="Ленинградская область, Россия"):
        self.location_name = location_name
        
    def get_boundary(self) -> gpd.GeoDataFrame:
        """
        Загрузка полигона границ исследуемой территории.
        """
        print(f"Загрузка границ для: {self.location_name}")
        gdf = ox.geocode_to_gdf(self.location_name)
        return gdf[['geometry', 'display_name']]

    def get_roads(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка графа дорожной сети и конвертация его в GeoDataFrame (ребра).
        Используется в качестве физических барьеров (линий разреза).
        Исключаются мелкие тропы.
        """
        print("Загрузка дорожной сети (магистрали, улицы)...")
        # custom_filter для исключения мелких дорожек, если необходимо
        custom_filter = '["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential"]'
        
        if boundary_poly is not None:
            # boundary_poly это shapely Polygon/MultiPolygon
            graph = ox.graph_from_polygon(boundary_poly, network_type='drive', custom_filter=custom_filter)
        else:
            graph = ox.graph_from_place(self.location_name, network_type='drive', custom_filter=custom_filter)
            
        _, edges = ox.graph_to_gdfs(graph)
        return edges

    def get_water(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка водных объектов (реки, каналы, озера).
        Также используются как физические барьеры.
        """
        print("Загрузка водных объектов...")
        tags = {'waterway': True, 'water': True, 'natural': 'water'}
        
        if boundary_poly is not None:
            water_gdf = ox.features_from_polygon(boundary_poly, tags)
        else:
            water_gdf = ox.features_from_place(self.location_name, tags)
            
        # Фильтруем только полигоны и линии
        water_gdf = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon', 'LineString', 'MultiLineString'])]
        return water_gdf

    def get_land_use(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка данных о типах землепользования (Land Use).
        """
        print("Загрузка данных землепользования (Land Use)...")
        tags = {'landuse': True}
        
        if boundary_poly is not None:
            lu_gdf = ox.features_from_polygon(boundary_poly, tags)
        else:
            lu_gdf = ox.features_from_place(self.location_name, tags)
            
        lu_gdf = lu_gdf[lu_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        return lu_gdf
        
    def get_amenities_and_buildings(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка зданий и инфраструктурных объектов для атрибутирования блоков.
        """
        print("Загрузка сервисов и зданий (Amenities & Buildings)...")
        tags = {'amenity': True, 'building': True, 'leisure': True}
        
        if boundary_poly is not None:
            amenities_gdf = ox.features_from_polygon(boundary_poly, tags)
        else:
            amenities_gdf = ox.features_from_place(self.location_name, tags)
            
        return amenities_gdf
