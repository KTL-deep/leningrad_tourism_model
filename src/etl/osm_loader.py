import osmnx as ox
import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon
import warnings

# Подавляем предупреждения от osmnx об устаревших параметрах (FutureWarning)
warnings.filterwarnings("ignore", category=FutureWarning, module="osmnx")

class OSMLoader:
    """
    Класс для загрузки открытых пространственных данных из OSM.
    Используется для подготовки базовых слоев (границы, сервисы, графы).
    """
    
    def __init__(self, locations=None):
        """
        :param locations: Строка или список строк с названиями локаций, или словари для структурированных запросов.
                          Например: ["Гатчинский район, Ленинградская область", {"city": "Pushkin", "state": "Saint Petersburg"}]
        """
        if locations is None:
            locations = ["Ленинградская область, Россия"]
        
        # Убедимся, что locations всегда список для удобства обработки
        if isinstance(locations, (str, dict)):
            self.locations = [locations]
        else:
            self.locations = list(locations)
        
        # Настраиваем osmnx: увеличиваем таймаут для больших запросов
        # Для совместимости с новыми и старыми версиями osmnx
        if hasattr(ox.settings, 'requests_timeout'):
            ox.settings.requests_timeout = 200
        else:
            ox.settings.timeout = 200

    def _resolve_loc(self, loc):
        """
        Разрешает элемент локации в (query_value, which_result).
        Элемент может быть строкой, словарем (структурированный запрос или с ключами query/which_result).
        """
        if isinstance(loc, dict):
            # Копируем словарь, чтобы не изменять исходные данные пользователя при pop/изменениях
            loc_copy = loc.copy()
            if "query" in loc_copy:
                return loc_copy["query"], loc_copy.get("which_result", 1)
            which = loc_copy.pop("which_result", 1) if "which_result" in loc_copy else 1
            return loc_copy, which
        elif isinstance(loc, tuple):
            return loc[0], loc[1]
        else:
            return loc, 1
        
    def get_boundary(self) -> gpd.GeoDataFrame:
        """
        Загрузка полигона(ов) границ исследуемой территории.
        """
        print(f"Загрузка границ для: {self.locations}")
        gdfs = []
        for loc in self.locations:
            try:
                q, which = self._resolve_loc(loc)
                gdf = ox.geocode_to_gdf(q, which_result=which)
                gdfs.append(gdf[['geometry', 'display_name']])
            except Exception as e:
                print(f"Ошибка загрузки границ для {loc}: {e}")
        
        if not gdfs:
            raise ValueError("Не удалось загрузить ни одной границы.")
            
        combined_gdf = pd.concat(gdfs, ignore_index=True)
        return combined_gdf

    def get_land_use(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка данных о типах землепользования (Land Use), воды и заболоченности.
        """
        print("Загрузка данных землепользования (Land Use, Природа, Вода, Болота)...")
        tags = {
            'landuse': True, 
            'natural': ['wood', 'water', 'wetland'], 
            'water': True, 
            'waterway': True
        }
        
        if boundary_poly is not None:
            # Используем union_all() с fallback на unary_union
            try:
                query_poly = boundary_poly.union_all()
            except AttributeError:
                query_poly = boundary_poly.unary_union
            lu_gdf = ox.features_from_polygon(query_poly, tags)
        else:
            gdfs = []
            for loc in self.locations:
                try:
                    q, _ = self._resolve_loc(loc)
                    gdfs.append(ox.features_from_place(q, tags))
                except Exception as e:
                    print(f"Ошибка загрузки POI для {loc}: {e}")
            lu_gdf = pd.concat(gdfs, ignore_index=True) if gdfs else gpd.GeoDataFrame()
            
        return lu_gdf
        
    def get_amenities_and_buildings(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка зданий и инфраструктурных объектов для атрибутирования блоков.
        """
        print("Загрузка объектов для классификации (Еда, Жилье, Транспорт, Культура)...")
        tags = {
            'amenity': ['cafe', 'restaurant', 'fast_food', 'bar', 'pub', 'food_court', 'hospital', 'clinic'], 
            'tourism': ['hotel', 'motel', 'hostel', 'guest_house', 'camp_site', 'museum', 'gallery', 'attraction', 'viewpoint', 'information'],
            'historic': True,  # ОКН, памятники, руины, мемориалы
            'leisure': ['park', 'garden', 'nature_reserve'],
            'highway': ['bus_stop'],
            'public_transport': ['station', 'platform'],
            'railway': ['station', 'halt']
        }
        
        if boundary_poly is not None:
            # Для надежности берем envelope объединения границ
            try:
                query_poly = boundary_poly.union_all().envelope
            except AttributeError:
                query_poly = boundary_poly.unary_union.envelope
            amenities_gdf = ox.features_from_polygon(query_poly, tags)
        else:
            gdfs = []
            for loc in self.locations:
                try:
                    q, _ = self._resolve_loc(loc)
                    amenities_gdf = ox.features_from_place(q, tags)
                    gdfs.append(amenities_gdf)
                except Exception as e:
                    print(f"Ошибка загрузки POI для {loc}: {e}")
            amenities_gdf = pd.concat(gdfs, ignore_index=True) if gdfs else gpd.GeoDataFrame()
            
        if not amenities_gdf.empty:
            # Преобразуем полигоны и линии в точки (центроиды) для унификации объектов POI
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning)
                if amenities_gdf.crs and not amenities_gdf.crs.is_projected:
                    utm_crs = amenities_gdf.estimate_utm_crs()
                    amenities_gdf['geometry'] = amenities_gdf.to_crs(utm_crs).geometry.centroid.to_crs(amenities_gdf.crs)
                else:
                    amenities_gdf['geometry'] = amenities_gdf.geometry.centroid

        return amenities_gdf

    def get_transport_graph(self, boundary_poly=None, network_type='drive'):
        """
        Загрузка транспортного графа для территории.
        network_type: 'drive', 'walk', 'all', etc.
        """
        print(f"Загрузка графа дорог (тип: {network_type})...")
        if boundary_poly is not None:
            try:
                query_poly = boundary_poly.union_all()
            except AttributeError:
                query_poly = boundary_poly.unary_union
            G = ox.graph_from_polygon(query_poly, network_type=network_type, simplify=True)
        else:
            if len(self.locations) == 1:
                q, _ = self._resolve_loc(self.locations[0])
                G = ox.graph_from_place(q, network_type=network_type, simplify=True)
            else:
                temp_bounds = self.get_boundary()
                try:
                    merged_poly = temp_bounds.union_all()
                except AttributeError:
                    merged_poly = temp_bounds.unary_union
                G = ox.graph_from_polygon(merged_poly, network_type=network_type, simplify=True)
        return G
