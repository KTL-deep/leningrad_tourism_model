import osmnx as ox
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon

class OSMLoader:
    """
    Класс для загрузки открытых пространственных данных из OSM.
    Используется для подготовки базовых слоев (границы, сервисы).
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



    def get_land_use(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка данных о типах землепользования (Land Use).
        """
        print("Загрузка данных землепользования (Land Use и Природа)...")
        tags = {'landuse': True, 'natural': ['wood', 'water'], 'water': True}
        
        if boundary_poly is not None:
            # ОПТИМИЗАЦИЯ: используем envelope (Bounding Box) для запроса к OSM. 
            # Сложные границы городов (как Павловск) заставляют Overpass API виснуть намертво. 
            # А точная обрезка в любом случае произойдет в генераторе блоков локально и быстро.
            query_poly = boundary_poly.envelope
            lu_gdf = ox.features_from_polygon(query_poly, tags)
        else:
            lu_gdf = ox.features_from_place(self.location_name, tags)
            
        lu_gdf = lu_gdf[lu_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        return lu_gdf
        
    def get_amenities_and_buildings(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка зданий и инфраструктурных объектов для атрибутирования блоков.
        """
        print("Загрузка объектов для классификации (Еда, Жилье, Транспорт)...")
        tags = {
            'amenity': True, 
            'building': True, 
            'leisure': True,
            'tourism': True,
            'highway': ['bus_stop'],
            'public_transport': True,
            'railway': ['station', 'halt']
        }
        
        if boundary_poly is not None:
            # ОПТИМИЗАЦИЯ: envelope для избежания Timeout-ов серверов OSM
            query_poly = boundary_poly.envelope
            amenities_gdf = ox.features_from_polygon(query_poly, tags)
        else:
            amenities_gdf = ox.features_from_place(self.location_name, tags)
            
        return amenities_gdf

